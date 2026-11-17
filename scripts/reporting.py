"""每批一份可读清单，仅为未解决的疑点生成截图。"""
import subprocess
from collections import Counter
from pathlib import Path

import store
import media
import workspace

REASONS = {
    'unknown_profile': '来源布局未确认，请指定 --profile 或校准 --region',
    'low_confidence': '目标 ID 识别置信度不足',
    'missing_victim': '受害者未读清，无法稳定区分重复播报与新击杀',
    'direction_conflict': '同行左侧存在低置信名字，击杀方向有疑点',
    'ambiguous_name': '目标 ID 与其他文字连续，尚不能确认是本人',
    'unstable_row': '同一播报行短时间内名字突变，可能为淡出或 OCR 变体，未计作新击杀',
    'possible_name': '名字与目标 ID 接近但不完全一致，未作为本人计杀',
    'same_player_name': '击杀者与受害者名字相同，可能为自杀或同名玩家，未计作可信击杀',
    'warmup_uncertain': '中央提示疑似热身但证据不足，未计作正式比赛击杀',
    'unknown_view_identity': '当前视角身份证据不足，未归属为可信击杀',
    'first_visible_unconfirmed': '开头已存在播报或有更早线索，不能把本次读到的时间直接当成新击杀',
    'delayed_attribution_unconfirmed': '可能是原视角人物的延迟击杀，需要核对先前动作和结果画面',
    'visual_role_unconfirmed': '红框里的主杀、助攻或受害位置未确认，暂不计杀',
    'visual_counter_unconfirmed': '播报候选尚未与独立击杀反馈对上，需要核对动作',
    'counter_feed_mismatch': '击杀计数增长与动作候选未对齐，不能直接补出击杀时间',
    'counter_change_unconfirmed': '切人或读数中断前出现计数增长，但尚未连续确认',
    'counter_timing_unconfirmed': '计数恢复前有较长识别空窗，需要查出实际击杀时间',
    'view_tracking_lost': '这一段没能确认当前视角，需看源画面判断人物与边界',
}


def review_image(path, item, out_dir):
    token = store.digest([store.identity(path), item.get('ts'), item.get('signature'), item['reason']])[:16]
    if item.get('screenshot_key') == token and Path(item.get('screenshot', '')).is_file():
        return Path(item['screenshot'])
    filename = f'{store.source_path_id(path)[:12]}_{token}.jpg'
    target = out_dir / 'review' / filename
    if target.is_file():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    input_args, offset = media.seek_input(path, item.get('ts', 0))
    seek = ['-ss', f'{offset:.6f}'] if offset else []
    result = subprocess.run(['ffmpeg', '-v', 'error', *input_args, *seek,
                             '-map', '0:v:0', '-frames:v', '1', '-an',
                             '-vf', 'scale=1280:-2', '-f', 'image2pipe', '-vcodec', 'mjpeg', '-'],
                            capture_output=True, timeout=60)
    if result.returncode or not result.stdout:
        raise ValueError(result.stderr.decode('utf-8', errors='replace')[-300:] or '未能取得截图')
    target.write_bytes(result.stdout)
    item['screenshot_key'] = token
    return target


def exit_code(files):
    if any(f['status'] == 'failed' or any(c['status'] == 'failed' for c in f.get('clips', [])) for f in files):
        return 1
    if any(f['status'] == 'needs_review' or f.get('review') for f in files):
        return 3
    return 0


def write_report(cfg, files, elapsed):
    stamp = store.now(cfg)
    root = workspace.report_dir(cfg)
    root.mkdir(parents=True, exist_ok=True)
    counts = Counter(c['status'] for f in files for c in f.get('clips', []))
    code = exit_code(files)
    if getattr(cfg, 'merge_result', {}).get('status') == 'failed':
        code = 1
    phase = '仅扫描' if cfg.scan_only else '仅提取有效扫描结果' if cfg.extract_only else '扫描与提取'
    lines = ['# SO2 处理清单', '', f'时间：{stamp.isoformat(timespec="seconds")}',
             f'执行阶段：{phase}。',
             f'本次 {len(files)} 个文件；耗时 {elapsed:.1f} 秒；退出码 {code}。',
             f'可用片段 {counts["generated"] + counts["reused"]}：新增 {counts["generated"]}，'
             f'复用 {counts["reused"]}；过滤 {counts["filtered"]}；提取失败 {counts["failed"]}。',
             '', f'本次参数：gap={cfg.kill_gap:g}s，min_kills={cfg.min_kills}，pre={cfg.pre:g}s，post={cfg.post:g}s。', '']
    if getattr(cfg, 'current_run_id', None):
        lines += [f'本次任务：`{cfg.current_run_id}`；归属模式：`{cfg.identity_mode}`。', '']
    merged = getattr(cfg, 'merge_result', None)
    if merged:
        lines += [f'集锦合并：{merged["status"]}；{merged.get("error") or ""}', '']
        if merged.get('path'):
            lines += [f'[原声集锦](<{Path(merged["path"]).as_posix()}>)', '']
    for file in files:
        lines += [f'## {Path(file["path"]).name}', '', f'源文件：`{file["path"]}`',
                  f'状态：{file["status"]}；预设：{file.get("profile", "未确认")}；'
                  f'可信击杀：{file.get("hit_count", 0)}；扫描缓存：{file.get("reused_scan", False)}。', '']
        if file.get('error'):
            lines += [f'失败原因：{file["error"]}', '']
        if file.get('scan_stats'):
            lines += [f'扫描统计：`{file["scan_stats"]}`', '']
        detection = file.get('phase', {})
        if detection.get('checked'):
            lines += [f'热身过滤：{len(detection.get("excluded_events", []))} 条候选击杀记录；'
                      f'另排除 {len(detection.get("excluded_review", []))} 条热身中的识别疑点。', '']
            for interval in detection.get('intervals', []):
                label = '已确认热身／赛前准备' if interval['kind'] == 'warmup' else '提示未确认（片段上下文避开此区间）'
                evidence = ' / '.join(dict.fromkeys(s['text'] for s in interval['evidence']))
                lines += [f'- {label} {interval["start"]:.3f}～{interval["end"]:.3f}s；依据：`{evidence}`']
            for event in detection.get('excluded_events', []):
                lines += [f'- 已过滤热身击杀 {event["ts"]:.3f}s：`{event.get("signature", event.get("text", ""))}`']
            lines.append('')
        elif cfg.hits is not None:
            lines += ['热身判定：使用手动时间，本次未执行 OCR 或自动过滤。', '']
        elif not cfg.filter_warmup:
            lines += ['热身判定：已显式保留热身，本次未自动过滤。', '']
        else:
            lines += ['热身判定：本次未取得有效的阶段结果。', '']
        for clip in file.get('clips', []):
            text = f'{clip["start"]:.3f}～{clip["end"]:.3f}s，{clip["kill_count"]} 杀，{clip["status"]}'
            if clip.get('path'):
                text += f'：[片段](<{Path(clip["path"]).as_posix()}>)'
            lines.append(f'- {text}')
            if clip.get('error'):
                lines.append(f'  原因：{clip["error"]}')
        lines.append('')
        for item in file.get('review', []):
            reason = REASONS.get(item['reason'], item['reason'])
            lines += [f'- 待复核 {item.get("ts", 0):.3f}s：{reason}；OCR：`{item.get("text", "")}`']
            if item.get('phase_evidence'):
                lines += [f'  阶段提示：`{" / ".join(s["text"] for s in item["phase_evidence"])}`']
            if getattr(cfg, 'defer_report_images', False):
                lines += ['  画面按需生成；当前 Agent 继续 prepare / evidence 复核。']
                continue
            if getattr(cfg, 'current_run_id', None):
                store.run_status(cfg, 'reporting', current_source=file['path'],
                                 current_evidence_ts=item.get('ts', 0))
            try:
                image = review_image(file['path'], item, root)
                item['screenshot'] = str(image)
                lines += ['', f'  ![疑点截图](<{image.as_posix()}>)', '']
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                item['screenshot_error'] = str(error)
                lines += [f'  截图未取得：{error}']
        if file.get('status') == 'needs_review':
            lines += ['', '处理：确认布局后，以同一素材重新运行并指定 `--profile`。', '']
        elif file['status'] == 'failed' or any(c['status'] == 'failed' for c in file.get('clips', [])):
            lines += ['', '重试：修正上述原因后，重跑原命令；有效扫描和已有片段自动复用。', '']
    target = root / f'report_{stamp:%H%M%S-%f}.md'
    target.write_text('\n'.join(lines), encoding='utf-8')
    print(f'可用片段 {counts["generated"] + counts["reused"]}（新增 {counts["generated"]}，复用 {counts["reused"]}），'
          f'过滤 {counts["filtered"]}，提取失败 {counts["failed"]}。', flush=True)
    print(f'清单：{target}\n退出码：{code}', flush=True)
    return code, target
