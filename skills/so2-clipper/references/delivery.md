# 自主复核与清单交付

用户提供输入和目标后，当前 Agent 在同一 Codex 任务内持续执行到交付。下面的复核与 JSON 决定由 Agent 完成，不是要求用户看图或填表。阶段之间不等待“继续”。质量优先，无固定完成时限；同一失败没有新证据或进展时停止重复尝试，继续独立项。

电脑／Codex 任务中断后从已保存清单恢复；不安装后台 AI 服务或配置开机唤起。原片缺失或关键内容始终无法确认时，可靠部分照常交付，整批标部分完成并在最终说明缺口，不将常规审核转交用户。

## 命令顺序

`$clip` 是本 Skill 的 `scripts/clip.ps1` 绝对路径。命令参数以 `--help` 为准。

```powershell
# 先发现当前结果，避免重复生成旧分组；有 plan 时直接继续该清单。
& $clip status --input 'D:\录像' --output-dir 'D:\成片'
& $clip run --input 'D:\录像' --output-dir 'D:\成片' --scan-only --defer-report-images
& $clip prepare --input 'D:\录像' --output-dir 'D:\成片'
# 指定目标人物时，prepare 加 --actors 'PlayerOne,玩家一'。
# 自定义运行目录时，run / prepare / status 都传同一 --state-dir。
& $clip evidence --plan '<返回的 plan.json>' --clip-id '<候选 clip_id>'
& $clip evidence --plan '<plan.json>' --source-id '<source_id>' --times '20,20.5,21,21.5'
& $clip deliver --plan '<plan.json>'
& $clip status --plan '<plan.json>'
```

`prepare` 创建独立清单、原扫描快照和复核队列，不进行新的 OCR。它在可信视角／阶段内生成不受 10 秒阈值拆分的候选；视角段仍不等于已确认回合。逐段核对人物、回合、阶段、首尾，缺证据则拆分、合并、补杀或排除。原扫描的 events/hits 保留，漏检事件直接记入最终 `kills`，不伪造 OCR 的 event_id。

清单创建时即写入 `curation/index.json`，首次导出前也能由 `status --input` 的 `active_review` 找回。相同源修订、目标人物和最低杀数再次 prepare 会复用同一清单。补扫改变扫描材料时，prepare 保存新的源快照与旧快照历史，保留已有片段决定和证据，加入新增候选／疑点，并将源级检查重置为 pending；Agent 必须将新线索与已有决定对照，不能把“复用清单”当作可以跳过新证据。

候选的 `round_id` 初始沿用视角段 `view_id`，只提供定位线索；保留为 `keep` 前仍需确认实际回合，不将该初始值当成工具已经完成了回合判断。

每个源都有 `review_queue`，包含原疑点及计数增量与播报数量不一致的位置；零候选的源也有 `overview_times`。不能只看候选卡片：查看源级上下文、计数变化和候选外可疑区间。需要时加密 `--times` 或查看连续短窗口。队列及源级检查没有结束时，不能把批次记为完整。

`evidence` 返回带时间、源身份和 SHA-256 的原尺寸帧、导航卡及 manifest。卡片最多每页六张；名字、图标和动作必须看清原图。图片生成成功不表示已看图。重复请求同一源修订与时间集合复用证据。源图与成片图分开保存，不互相冒充。

## Agent 填写的决定

保留 `prepare` 产生的源身份、源快照和原扫描时间。修改清单只改本次判断相关字段，不修改原录像、原 OCR 或历史回执。

- `settings.actors`：确切目标人物集合；空数组表示当前视角全部人物，允许已确认视觉归属的稳定匿名标识。指定目标时匿名不算命中；只有画面已确认名字与该视角的对应关系后才能填写该名字。昵称变体由画面确认后在本批明确列入，不能按相似名字猜。
- `settings.min_kills`：默认 2；`settings.categories` 可将标准大类映射为本批文件夹名，如 `{"狙击枪":"狙击"}`。
- `sources[].review`：`status=reviewed`、具体 `reason`、实际看过的源图路径 `evidence`、无法解决的 `unresolved`。源级检查包括完整同回合范围、目标人物与遗漏，不拿末端 HUD 总数当杀数。
- `sources[].review_queue[]`：自行处理后写 `status=resolved`、`resolution` 和 `evidence`；无法确认写 `unresolved` 与具体原因。不因队列为空跳过源级检查。
- `clips[].status`：`pending`、`unresolved`、`keep`、`reject` 或 `duplicate`。最终决定填写 `reason`；重复条目用 `duplicate_of` 指向本清单保留的 clip_id，并提供本源比较画面。跨源重复对照多个对应时刻、HUD 和击杀序列，由 Agent 决定。
- `keep` 条目填写最终源时间 `start/end`、`actor`、经复核的 `round_id`、`kills` 和首尾 `boundary_evidence`。每次击杀的格式为：

```json
{
  "ts": 81.0,
  "actor": "PlayerOne",
  "weapon": "狙击枪",
  "evidence": ["实际动作前图片路径", "实际结果后图片路径"]
}
```

逐杀图片须在该源 evidence manifest 中登记，包含 ts 前及后各不超过 2 秒的画面；最终首尾各需距边界 1 秒内的源图。字段只能填实际看过的图片，机器检查仅核验身份／时间／哈希，不代替画面语义判断。

武器仅用：狙击枪、步枪、冲锋枪、手枪、霰弹枪、投掷物、近战。工具按逐杀武器自动决定大类；跨大类才归混合。换枪但没杀人不影响分类。未知武器保持 unresolved，不用“混合”掩盖未知。

源时钟错位时按 [时间校准](calibration.md#源时间与扫描时间) 验证。保留 `scan_times`，可另记 `source_times`、`time_mapping` 及锚点；最终 start/end、kills.ts 和抽图全部使用验证过的源时间。修订的回合不得跨真实切人、复活、回合重置或热身；C4 自杀可以作为结局，不能增加 kills。

对补回的长间隔击杀，把旧短片标为 duplicate 并指向完整片段；不要同时交付重叠的同一击杀。对源本身缺失的过程保留缺口，不能编造 kills。已交付过的 clip_id 不从清单静默删除，改成 reject／duplicate 后工具保留备份并更新当前索引。

## 导出、成片复核与续办

`deliver` 冻结本次清单与索引快照，验证源身份、逐杀图片、范围和人物，再逐项执行。文件名与武器目录由真实 kills 和区间生成。相同内容复用现存字节；只改分类名称时移动；范围／内容变化才重提。旧版本移到独立 state_dir 下的备份，不进入正式成片目录。没有当前任务恢复依据的目标冲突保持失败，不覆盖。

每条新增成片检查视频、音轨、尺寸、时长、首尾及完整解码；保存文件身份与哈希后生成输出画面。占用或某条失败不阻塞其他独立项。状态逐项写入 `plan.json` 同目录的 `delivery.json`；同一 state_dir 写入锁由系统随进程退出释放。

第一次 deliver 通常返回 3：输出已生成，仍需当前 Agent 查看 `plan.output_reviews` 中的成片图。检查首尾、击杀动作及源时间对齐后，将对应条目的 `status` 改为 `confirmed`，填写具体 `reason`，保留工具绑定的 identity、sha256、evidence_manifest。发现问题先修改最终范围／决定并再次导出；不要确认错误输出。

```powershell
# 如需更多成片画面，时间相对于成片；会重新将该成片复核设为 pending。
& $clip evidence --plan '<plan.json>' --clip-id '<clip_id>' --output-clip --times '0,8,15,20'
# Agent 完成成片看图回填后，重复调用复用文件并最终校验索引。
& $clip deliver --plan '<plan.json>'
```

退出 0 表示本清单的源／候选决定、媒体检查、成片画面复核和索引读回均完成；仍不表示实际连续播放、试听或用户验收。3 表示尚有复核／证据缺口，1 表示有执行失败。当前 Agent 继续处理能够解决的项目；无进展的客观缺口最终按部分完成汇报。

新清单交付后，扫描条目的 `curated_delivery` 保存 plan/journal；普通 run／extract-only 返回续办指针，禁止旧事件重新分组。需要重新扫描时 scan-only 仍可用，指针保留；源变化会使旧证据失效。历史任务仅有非结构化说明时，先读旧清单并按本格式建立有依据的决定，不能声称已经自动迁移。

扫描单独写 `runs/<run_id>/scan-stage.json`，报告前即落盘；`status.json` 显示扫描、提取、汇总及本次退出状态。需要时：

```powershell
& $clip status --state-dir '<state_dir>' --run-id '<run_id>'
```

阶段是磁盘快照，活跃状态另核对实际进程。`--defer-report-images` 让扫描总回执不等待全部疑点截图；后续 evidence 按当前复核范围取图。不得用旧回执证明本次完成。
